#!/bin/sh
# setup-pinned-sources.sh — wire up the pinned protocol sources locally.
#
# Clones/updates mojomast/workflow and mojomast/hyvv at the revisions CI pins
# into $RIZZSTACK_PINNED_DIR (default ~/.rizzstack/pinned), makes gitleaks
# available, and prints the exact export lines the fail-closed suites need.
#
# Usage: sh scripts/setup-pinned-sources.sh
#   stdout: exactly three eval-ready lines (WORKFLOW_SRC, HYVV_SRC, GITLEAKS)
#   stderr: progress notes and, on failure, an actionable fix
#
# Idempotent. Never deletes, resets or force-pushes an existing checkout.

set -u

TAG="setup-pinned-sources"

WORKFLOW_URL="https://github.com/mojomast/workflow.git"
WORKFLOW_BRANCH="main"
WORKFLOW_PIN="ae9c080262ab593e7643e2fba79cd51a018f25df"
HYVV_URL="https://github.com/mojomast/hyvv.git"
HYVV_BRANCH="main"
HYVV_PIN="fdd235446e8fb28f8074fa06ea5c6ee1e466e129"

GITLEAKS_VERSION="8.28.0"
GITLEAKS_SHA256_LINUX_X64="a65b5253807a68ac0cafa4414031fd740aeb55f54fb7e55f386acb52e6a840eb"
GITLEAKS_SHA256_LINUX_ARM64="eff65261156100e5d94a6b3dec313d532fddfe19ae1590bf7a2b4f2699128356"
GITLEAKS_SHA256_DARWIN_ARM64="d942f3ad147250c9edbaab3fed9e482f98d3b59ba10ae97b8d75647e3ade492c"
GITLEAKS_SHA256_DARWIN_X64="edf5a507008b0d2ef4959575772772770586409c1f6f74dabf19cbe7ec341ced"

die() {
    printf '%s: ERROR: %s\n' "$TAG" "$1" >&2
    if [ "$#" -ge 2 ] && [ -n "$2" ]; then
        printf '%s: FIX: %s\n' "$TAG" "$2" >&2
    fi
    exit 1
}

note() {
    printf '%s: %s\n' "$TAG" "$1" >&2
}

sanitize_text() {
    printf '%s' "$1" | sed -e 's#//[^/]*@#//#g'
}

normalize_git_url() {
    printf '%s' "$1" | sed -e 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##' \
        -e 's#^git@##' -e 's#^[^/@]*@##' \
        -e 's#:#/#' -e 's#\.git$##' -e 's#/$##'
}

if [ "$#" -gt 0 ]; then
    note "ignoring command-line arguments; configuration comes from the environment"
fi

command -v git >/dev/null 2>&1 ||
    die "git is not available on PATH" "install git (e.g. 'apt install git' or 'brew install git') and re-run"

PINNED_DIR="${RIZZSTACK_PINNED_DIR:-$HOME/.rizzstack/pinned}"
case "$PINNED_DIR" in
    /*) ;;
    *) PINNED_DIR="$(pwd)/$PINNED_DIR" ;;
esac
case "$PINNED_DIR" in
    *'"'*|*'`'*|*'$'*)
        die "RIZZSTACK_PINNED_DIR contains shell metacharacters: the export lines could not be emitted safely" "choose a plain path (no quotes, \$ or backticks)"
        ;;
esac
mkdir -p "$PINNED_DIR" ||
    die "cannot create $PINNED_DIR" "check that the parent directory is writable (or set RIZZSTACK_PINNED_DIR), then re-run"
PINNED_DIR=$(cd "$PINNED_DIR" && pwd -P) ||
    die "cannot enter $PINNED_DIR" "check permissions on the directory (or set RIZZSTACK_PINNED_DIR), then re-run"

WORKFLOW_DIR="$PINNED_DIR/workflow"
HYVV_DIR="$PINNED_DIR/hyvv"

ensure_repo() {
    _er_name="$1"
    _er_url="$2"
    _er_branch="$3"
    _er_pin="$4"
    shift 4
    _er_dir="$PINNED_DIR/$_er_name"

    if [ ! -d "$_er_dir" ]; then
        note "$_er_name: cloning into $_er_dir"
        _er_err=$(git clone --quiet "$_er_url" "$_er_dir" 2>&1) ||
            die "${_er_name}: git clone failed: $(sanitize_text "$_er_err")" "check network access to the remote and free disk space under $PINNED_DIR, then re-run; this script never deletes or rewrites an existing checkout"
    elif [ ! -d "$_er_dir/.git" ]; then
        die "${_er_name}: $_er_dir exists but is not a git checkout" "move or remove that directory yourself (this script never deletes or resets it), then re-run"
    else
        _er_remote=$(git -C "$_er_dir" remote get-url origin 2>/dev/null) ||
            die "${_er_name}: $_er_dir has no origin remote" "point RIZZSTACK_PINNED_DIR at a fresh location, or move/remove the directory yourself, then re-run"
        if [ "$(normalize_git_url "$_er_remote")" != "$(normalize_git_url "$_er_url")" ]; then
            die "${_er_name}: $_er_dir tracks a different origin than $_er_name's upstream" "point RIZZSTACK_PINNED_DIR at a fresh location, or move/remove the directory yourself, then re-run"
        fi
    fi

    _er_head=$(git -C "$_er_dir" rev-parse HEAD 2>/dev/null) || _er_head=""
    if [ "$_er_head" != "$_er_pin" ]; then
        note "$_er_name: moving to the pinned revision"
        _er_err=$(git -C "$_er_dir" fetch --quiet origin "refs/heads/$_er_branch" 2>&1) ||
            note "$_er_name: fetch failed (offline?); continuing with already-fetched objects"
        _er_err=$(git -C "$_er_dir" checkout --quiet "$_er_pin" 2>&1) ||
            die "${_er_name}: cannot check out the pinned revision in $_er_dir: $(sanitize_text "$_er_err")" "the checkout likely carries local changes that conflict with the pin; commit or discard them yourself (this script never resets), or move/remove the directory and re-run; if the revision itself is gone from the remote, ask the owner to re-pin (see docs/pinned-sources.md)"
    else
        note "$_er_name: already at the pinned revision"
    fi

    _er_head=$(git -C "$_er_dir" rev-parse HEAD 2>/dev/null) ||
        die "${_er_name}: $_er_dir is not a usable git checkout" "move or remove that directory yourself, then re-run"
    [ "$_er_head" = "$_er_pin" ] ||
        die "${_er_name}: $_er_dir is at ${_er_head:-unknown}, not the pinned ${_er_pin}" "re-run this script; if it cannot move the checkout, commit or discard the local changes yourself, or move/remove the directory"

    for _er_marker in "$@"; do
        [ -f "$_er_dir/$_er_marker" ] ||
            die "${_er_name}: the pinned checkout is incomplete: $_er_marker is missing" "if $_er_dir holds a stale or partial checkout, move or remove it yourself and re-run; otherwise report that pinned revision ${_er_pin} no longer contains $_er_marker"
    done
    note "$_er_name: verified at the pinned revision with $_er_marker present"
}

ensure_repo workflow "$WORKFLOW_URL" "$WORKFLOW_BRANCH" "$WORKFLOW_PIN" tools/recovery.py schemas/recovery.schema.json
ensure_repo hyvv "$HYVV_URL" "$HYVV_BRANCH" "$HYVV_PIN" provisioning.py

GITLEAKS_PATH=""
case "${GITLEAKS:-}" in
    "")
        ;;
    *)
        if [ -x "$GITLEAKS" ]; then
            GITLEAKS_PATH="$GITLEAKS"
            note "gitleaks: using the GITLEAKS already set in the environment"
        else
            note "gitleaks: ignoring the GITLEAKS in the environment; it is not an executable file"
        fi
        ;;
esac

if [ -z "$GITLEAKS_PATH" ]; then
    _gl_found=$(command -v gitleaks 2>/dev/null) || _gl_found=""
    if [ -n "$_gl_found" ]; then
        case "$_gl_found" in
            /*) ;;
            *) _gl_found="$(pwd)/$_gl_found" ;;
        esac
        GITLEAKS_PATH="$_gl_found"
        note "gitleaks: found on PATH"
    fi
fi

if [ -z "$GITLEAKS_PATH" ]; then
    _gl_cache="$PINNED_DIR/bin/gitleaks"
    if [ -x "$_gl_cache" ] && "$_gl_cache" version >/dev/null 2>&1; then
        GITLEAKS_PATH="$_gl_cache"
        note "gitleaks: reusing the pinned copy at $PINNED_DIR/bin/gitleaks"
    fi
fi

if [ -z "$GITLEAKS_PATH" ]; then
    _gl_os=$(uname -s 2>/dev/null) || _gl_os=""
    _gl_arch=$(uname -m 2>/dev/null) || _gl_arch=""
    case "${_gl_os}/${_gl_arch}" in
        Linux/x86_64 | Linux/amd64)
            _gl_asset="linux_x64"
            _gl_sha="$GITLEAKS_SHA256_LINUX_X64"
            ;;
        Linux/arm64 | Linux/aarch64)
            _gl_asset="linux_arm64"
            _gl_sha="$GITLEAKS_SHA256_LINUX_ARM64"
            ;;
        Darwin/arm64)
            _gl_asset="darwin_arm64"
            _gl_sha="$GITLEAKS_SHA256_DARWIN_ARM64"
            ;;
        Darwin/x86_64 | Darwin/amd64)
            _gl_asset="darwin_x64"
            _gl_sha="$GITLEAKS_SHA256_DARWIN_X64"
            ;;
        *)
            die "gitleaks is not installed and no pre-verified download exists for ${_gl_os:-unknown}/${_gl_arch:-unknown}" "install gitleaks yourself (see https://github.com/gitleaks/gitleaks/releases) and either put it on PATH or export GITLEAKS=/path/to/gitleaks, then re-run"
            ;;
    esac

    command -v tar >/dev/null 2>&1 ||
        die "tar is not available on PATH; gitleaks cannot be installed into $PINNED_DIR/bin" "install tar, or install gitleaks yourself and export GITLEAKS=/path/to/gitleaks, then re-run"
    if command -v sha256sum >/dev/null 2>&1; then
        :
    elif command -v shasum >/dev/null 2>&1; then
        :
    else
        die "neither sha256sum nor shasum is available; the gitleaks download cannot be checksum-verified" "install one of them, or install gitleaks yourself and export GITLEAKS=/path/to/gitleaks, then re-run"
    fi
    if command -v curl >/dev/null 2>&1; then
        _gl_fetch="curl"
    elif command -v wget >/dev/null 2>&1; then
        _gl_fetch="wget"
    else
        die "neither curl nor wget is available; gitleaks cannot be downloaded" "install curl or wget, or install gitleaks yourself and export GITLEAKS=/path/to/gitleaks, then re-run"
    fi

    _gl_bin="$PINNED_DIR/bin"
    mkdir -p "$_gl_bin" ||
        die "cannot create $_gl_bin" "check permissions under $PINNED_DIR, then re-run"
    _gl_tgz="$_gl_bin/.gitleaks-$$-download.tar.gz"
    _gl_url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_${_gl_asset}.tar.gz"
    note "gitleaks: downloading v${GITLEAKS_VERSION} (${_gl_asset})"
    if [ "$_gl_fetch" = "curl" ]; then
        _gl_err=$(curl -sSfL -o "$_gl_tgz" "$_gl_url" 2>&1) ||
            { rm -f "$_gl_tgz"; die "gitleaks download failed: $(sanitize_text "$_gl_err")" "check network access to github.com; or install gitleaks yourself and export GITLEAKS=/path/to/gitleaks, then re-run"; }
    else
        _gl_err=$(wget -q -O "$_gl_tgz" "$_gl_url" 2>&1) ||
            { rm -f "$_gl_tgz"; die "gitleaks download failed: $(sanitize_text "$_gl_err")" "check network access to github.com; or install gitleaks yourself and export GITLEAKS=/path/to/gitleaks, then re-run"; }
    fi
    _gl_got=$(sha256sum "$_gl_tgz" 2>/dev/null | sed -n 's/^\([0-9a-fA-F]\{64\}\).*/\1/p')
    if [ -z "$_gl_got" ]; then
        _gl_got=$(shasum -a 256 "$_gl_tgz" 2>/dev/null | sed -n 's/^\([0-9a-fA-F]\{64\}\).*/\1/p')
    fi
    if [ "$_gl_got" != "$_gl_sha" ]; then
        rm -f "$_gl_tgz"
        die "the gitleaks tarball failed its checksum verification" "the download may be truncated or tampered with; re-run later, or install gitleaks yourself and export GITLEAKS=/path/to/gitleaks"
    fi
    _gl_err=$(tar -xzf "$_gl_tgz" -C "$_gl_bin" gitleaks 2>&1) ||
        { rm -f "$_gl_tgz"; die "gitleaks tarball could not be extracted: $(sanitize_text "$_gl_err")" "remove $_gl_tgz yourself and re-run, or install gitleaks yourself and export GITLEAKS=/path/to/gitleaks"; }
    rm -f "$_gl_tgz"
    chmod 755 "$_gl_bin/gitleaks" ||
        die "cannot mark $_gl_bin/gitleaks executable" "check permissions under $PINNED_DIR, then re-run"
    GITLEAKS_PATH="$_gl_bin/gitleaks"
    note "gitleaks: installed v${GITLEAKS_VERSION} (${_gl_asset}) into $PINNED_DIR/bin"
fi

"$GITLEAKS_PATH" version >/dev/null 2>&1 ||
    die "the gitleaks at $GITLEAKS_PATH does not run on this host" "install a gitleaks build for this platform and export GITLEAKS=/path/to/gitleaks, then re-run"

for _p in "$WORKFLOW_DIR" "$HYVV_DIR" "$GITLEAKS_PATH"; do
    case "$_p" in
        *'"'*|*'`'*|*'$'*)
            die "refusing to emit an export line for a path containing shell metacharacters" "keep the pinned sources under a plain path (no quotes, \$ or backticks)"
            ;;
    esac
done

printf 'export WORKFLOW_SRC="%s"\n' "$WORKFLOW_DIR"
printf 'export HYVV_SRC="%s"\n' "$HYVV_DIR"
printf 'export GITLEAKS="%s"\n' "$GITLEAKS_PATH"

note "done; eval the three stdout lines in your shell (eval \"\$(sh scripts/setup-pinned-sources.sh)\")"
note "add REQUIRE_WORKFLOW=1 REQUIRE_HYVV=1 when running the suites to make a missing source fail closed instead of skip (CI sets both)"
