#!/usr/bin/env bash
set -Eeuo pipefail

# ═══════════════════════════════════════════════════════════════════════
# bootstrap.sh — 裸机服务器一键引导 (Phase 0)
# ═══════════════════════════════════════════════════════════════════════
#
# 在 start.sh 之前运行，解决"python3 都没有"的鸡生蛋问题。
# 纯 bash 实现，不依赖 python/R/conda。
#
# Usage:
#   bash ops/bootstrap.sh              # 交互式（有确认提示）
#   bash ops/bootstrap.sh --yes        # 非交互，全自动
#   bash ops/bootstrap.sh --dry-run    # 只检查，不安装
#   bash ops/bootstrap.sh --skip-ollama        # 跳过 Ollama
#   bash ops/bootstrap.sh --skip-conda         # 跳过 conda/miniconda
#   bash ops/bootstrap.sh --skip-archs4        # 跳过 ARCHS4 H5 下载
#   bash ops/bootstrap.sh --ollama-host URL    # 远程 Ollama (不装本地)
#
# 完成后接:
#   bash ops/start.sh setup    # Phase 1-2: venv + pip + R packages
#   bash ops/start.sh check    # 验证全部就绪
# ═══════════════════════════════════════════════════════════════════════

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()     { printf "${GREEN}  ✓${NC} %s\n" "$*"; }
fail()   { printf "${RED}  ✗${NC} %s\n" "$*"; }
warn()   { printf "${YELLOW}  ⚠${NC} %s\n" "$*"; }
info()   { printf "${BLUE}  ℹ${NC} %s\n" "$*"; }
step()   { printf "\n${CYAN}${BOLD}── %s${NC}\n" "$*"; }
header() {
    printf "\n${BLUE}═══════════════════════════════════════════════${NC}\n"
    printf "${BLUE}  %s${NC}\n" "$*"
    printf "${BLUE}═══════════════════════════════════════════════${NC}\n\n"
}

# ── Parse Args ──
AUTO_YES=0
DRY_RUN=0
SKIP_OLLAMA=0
SKIP_CONDA=0
SKIP_ARCHS4=0
REMOTE_OLLAMA=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)          AUTO_YES=1; shift ;;
        --dry-run)         DRY_RUN=1; shift ;;
        --skip-ollama)     SKIP_OLLAMA=1; shift ;;
        --skip-conda)      SKIP_CONDA=1; shift ;;
        --skip-archs4)     SKIP_ARCHS4=1; shift ;;
        --ollama-host)     REMOTE_OLLAMA="$2"; SKIP_OLLAMA=1; shift 2 ;;
        -h|--help)
            sed -n '/^# Usage:/,/^# ═══/p' "$0" | head -n -1 | sed 's/^# //'
            exit 0
            ;;
        *) fail "Unknown option: $1"; exit 1 ;;
    esac
done

confirm() {
    if [[ "${AUTO_YES}" -eq 1 ]]; then return 0; fi
    printf "${YELLOW}  → %s [Y/n] ${NC}" "$1"
    read -r ans
    case "${ans}" in
        ""|[Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

run_or_dry() {
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        info "[dry-run] $*"
        return 0
    fi
    "$@"
}

# ── Detect OS ──
detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        case "${ID}" in
            ubuntu|debian|pop|linuxmint) echo "debian" ;;
            centos|rhel|rocky|almalinux|fedora|amzn) echo "rhel" ;;
            arch|manjaro) echo "arch" ;;
            *) echo "unknown-${ID}" ;;
        esac
    elif [[ "$(uname)" == "Darwin" ]]; then
        echo "macos"
    else
        echo "unknown"
    fi
}

# ── Detect privilege ──
has_sudo() {
    if [[ "$(id -u)" -eq 0 ]]; then return 0; fi
    command -v sudo &>/dev/null && sudo -n true 2>/dev/null
}

SUDO=""
if [[ "$(id -u)" -ne 0 ]] && command -v sudo &>/dev/null; then
    SUDO="sudo"
fi

# ═══════════════════════════════════════════════════════════════════════
header "Drug Repurposing Platform — Bootstrap (Phase 0)"

OS_TYPE="$(detect_os)"
info "OS detected: ${OS_TYPE}"
info "Root dir:    ${ROOT_DIR}"
info "User:        $(whoami)"
info "Dry run:     ${DRY_RUN}"
echo ""

ISSUES=()
INSTALLED=()

# ═══════════════════════════════════════════════════════════════════════
# 0. Pre-flight: hardware checks (RAM / disk / GPU)
# ═══════════════════════════════════════════════════════════════════════

step "0. Hardware Pre-flight"

# ── RAM ──
get_ram_gb() {
    if [[ "${OS_TYPE}" == "macos" ]]; then
        sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.1f", $1/1024/1024/1024}'
    elif [[ -f /proc/meminfo ]]; then
        awk '/MemTotal/{printf "%.1f", $2/1024/1024}' /proc/meminfo
    else
        echo "0"
    fi
}

RAM_GB="$(get_ram_gb)"
if (( $(echo "${RAM_GB} < 8" | bc -l 2>/dev/null || echo 1) )); then
    fail "RAM: ${RAM_GB} GB — 最低需要 8GB（推荐 16GB+，Ollama 推理 + ARCHS4 读取）"
    ISSUES+=("RAM < 8GB")
elif (( $(echo "${RAM_GB} < 16" | bc -l 2>/dev/null || echo 0) )); then
    warn "RAM: ${RAM_GB} GB — 能跑但偏小，Ollama 推理可能较慢"
else
    ok "RAM: ${RAM_GB} GB"
fi

# ── Disk ──
get_free_disk_gb() {
    df -BG "${ROOT_DIR}" 2>/dev/null | awk 'NR==2{gsub(/G/,"",$4); print $4}' ||
    df -g "${ROOT_DIR}" 2>/dev/null | awk 'NR==2{print $4}' ||
    echo "0"
}

DISK_GB="$(get_free_disk_gb)"
NEED_DISK=60  # ~45GB ARCHS4 + venvs + R + models + workspace
if [[ "${SKIP_ARCHS4}" -eq 1 ]]; then NEED_DISK=15; fi

if [[ "${DISK_GB}" -lt "${NEED_DISK}" ]]; then
    fail "Disk free: ${DISK_GB} GB — 需要至少 ${NEED_DISK} GB"
    ISSUES+=("Disk < ${NEED_DISK}GB")
else
    ok "Disk free: ${DISK_GB} GB"
fi

# ── GPU ──
if command -v nvidia-smi &>/dev/null; then
    GPU_INFO="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "unknown")"
    ok "GPU detected: ${GPU_INFO}"
    ok "Ollama 会自动使用 GPU 加速 LLM 推理"
else
    warn "未检测到 GPU (nvidia-smi not found)"
    warn "Ollama 会用 CPU 跑 qwen2.5:7b — 推理速度约慢 5-10x"
    warn "如果服务器有 GPU，请先安装 NVIDIA 驱动 + CUDA"
fi

# ═══════════════════════════════════════════════════════════════════════
# 1. System packages: python3, python3-venv, R, C libs
# ═══════════════════════════════════════════════════════════════════════

step "1. System Packages"

install_debian() {
    info "Installing via apt..."
    run_or_dry ${SUDO} apt-get update -qq
    run_or_dry ${SUDO} apt-get install -y \
        python3 python3-venv python3-pip python3-dev \
        r-base r-base-dev \
        libcurl4-openssl-dev libxml2-dev libssl-dev libfontconfig1-dev \
        libharfbuzz-dev libfribidi-dev libfreetype-dev libpng-dev libtiff-dev \
        build-essential gfortran wget curl git bc \
        pciutils lshw zstd
}

install_rhel() {
    info "Installing via yum/dnf..."
    local PM="yum"
    command -v dnf &>/dev/null && PM="dnf"
    run_or_dry ${SUDO} ${PM} install -y epel-release 2>/dev/null || true
    run_or_dry ${SUDO} ${PM} install -y \
        python3 python3-devel python3-pip \
        R R-core-devel \
        libcurl-devel libxml2-devel openssl-devel \
        harfbuzz-devel fribidi-devel freetype-devel libpng-devel libtiff-devel \
        gcc gcc-c++ gcc-gfortran make wget curl git bc
}

install_macos() {
    if ! command -v brew &>/dev/null; then
        warn "Homebrew not found. Install: https://brew.sh"
        ISSUES+=("no Homebrew")
        return 1
    fi
    info "Installing via Homebrew..."
    run_or_dry brew install python@3.11 r wget curl bc
}

# Check what's already present
check_system_packages() {
    local need_install=0

    if command -v python3 &>/dev/null; then
        PY_VER="$(python3 --version 2>&1)"
        ok "python3: ${PY_VER}"
    else
        fail "python3: not found"
        need_install=1
    fi

    # python3-venv
    if command -v python3 &>/dev/null; then
        if python3 -m venv --help &>/dev/null 2>&1; then
            ok "python3-venv: available"
        else
            fail "python3-venv: missing"
            need_install=1
        fi
    fi

    if command -v Rscript &>/dev/null; then
        R_VER="$(Rscript --version 2>&1 | head -1)"
        ok "R: ${R_VER}"
    else
        fail "R: not found"
        need_install=1
    fi

    if command -v gcc &>/dev/null; then
        ok "gcc: $(gcc --version | head -1)"
    else
        fail "gcc: not found (R 包编译需要)"
        need_install=1
    fi

    # C libs (quick header check for Debian/RHEL)
    if [[ "${OS_TYPE}" == "debian" ]]; then
        if dpkg -s libcurl4-openssl-dev &>/dev/null 2>&1; then
            ok "libcurl-dev: installed"
        else
            fail "libcurl-dev: missing (R curl 包编译需要)"
            need_install=1
        fi
    fi

    return ${need_install}
}

if check_system_packages; then
    ok "系统包已齐全，跳过安装"
else
    echo ""
    if confirm "安装缺失的系统包？(需要 sudo)"; then
        case "${OS_TYPE}" in
            debian) install_debian ;;
            rhel)   install_rhel ;;
            macos)  install_macos ;;
            *)
                fail "不支持的 OS: ${OS_TYPE}，请手动安装 python3, R, gcc"
                ISSUES+=("unsupported OS for auto-install")
                ;;
        esac

        # Verify after install
        if command -v python3 &>/dev/null; then
            INSTALLED+=("python3")
            ok "python3 安装成功"
        else
            fail "python3 安装失败"
            ISSUES+=("python3 install failed")
        fi
        if command -v Rscript &>/dev/null; then
            INSTALLED+=("R")
            ok "R 安装成功"
        else
            fail "R 安装失败"
            ISSUES+=("R install failed")
        fi
    else
        warn "跳过系统包安装 — start.sh setup 可能会失败"
        ISSUES+=("system packages skipped")
    fi
fi

# ═══════════════════════════════════════════════════════════════════════
# 2. Conda / Miniconda (dsmeta 环境需要)
# ═══════════════════════════════════════════════════════════════════════

step "2. Conda (dsmeta pipeline)"

if [[ "${SKIP_CONDA}" -eq 1 ]]; then
    info "跳过 conda 安装 (--skip-conda)"
    info "dsmeta 将使用 venv fallback (需要系统 R 已安装)"
else
    if command -v conda &>/dev/null; then
        CONDA_VER="$(conda --version 2>&1 | tail -1)"
        ok "conda: ${CONDA_VER}"

        # Check if dsmeta env exists
        if conda env list 2>/dev/null | grep -q "^dsmeta "; then
            ok "conda env 'dsmeta' already exists"
        else
            info "conda env 'dsmeta' not found"
            if confirm "创建 conda dsmeta 环境？(包含 R + Bioconductor，约 10-20 分钟)"; then
                ENV_YML="${ROOT_DIR}/dsmeta_signature_pipeline/environment.yml"
                if [[ -f "${ENV_YML}" ]]; then
                    run_or_dry conda env create -n dsmeta -f "${ENV_YML}"
                    INSTALLED+=("conda-dsmeta")
                else
                    fail "environment.yml not found: ${ENV_YML}"
                    ISSUES+=("dsmeta environment.yml missing")
                fi
            fi
        fi
    else
        warn "conda not found"
        if confirm "安装 Miniconda？(推荐，dsmeta 管线最佳方案)"; then
            MINICONDA_DIR="${HOME}/miniconda3"
            ARCH="$(uname -m)"
            case "${OS_TYPE}" in
                macos)
                    if [[ "${ARCH}" == "arm64" ]]; then
                        MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-arm64.sh"
                    else
                        MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-x86_64.sh"
                    fi
                    ;;
                *)
                    if [[ "${ARCH}" == "aarch64" ]]; then
                        MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh"
                    else
                        MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
                    fi
                    ;;
            esac

            info "Downloading Miniconda..."
            INSTALLER="/tmp/miniconda_installer.sh"
            run_or_dry curl -fsSL "${MINICONDA_URL}" -o "${INSTALLER}"
            run_or_dry bash "${INSTALLER}" -b -p "${MINICONDA_DIR}"
            rm -f "${INSTALLER}"

            # Add to PATH for this session
            export PATH="${MINICONDA_DIR}/bin:${PATH}"

            if command -v conda &>/dev/null; then
                ok "Miniconda 安装成功"
                INSTALLED+=("miniconda")

                # Init for future shells
                run_or_dry conda init bash 2>/dev/null || true
                run_or_dry conda init zsh 2>/dev/null || true

                # Create dsmeta env
                ENV_YML="${ROOT_DIR}/dsmeta_signature_pipeline/environment.yml"
                if [[ -f "${ENV_YML}" ]]; then
                    info "创建 conda dsmeta 环境..."
                    run_or_dry conda env create -n dsmeta -f "${ENV_YML}"
                    INSTALLED+=("conda-dsmeta")
                fi
            else
                fail "Miniconda 安装失败"
                ISSUES+=("miniconda install failed")
            fi
        else
            info "跳过 conda — dsmeta 将使用 venv + 系统 R fallback"
        fi
    fi
fi

# ═══════════════════════════════════════════════════════════════════════
# 3. Ollama (本地 LLM 推理引擎)
# ═══════════════════════════════════════════════════════════════════════

step "3. Ollama (LLM inference)"

if [[ "${SKIP_OLLAMA}" -eq 1 ]]; then
    if [[ -n "${REMOTE_OLLAMA}" ]]; then
        info "使用远程 Ollama: ${REMOTE_OLLAMA}"
        info "Direction A 和 B 都需要 LLM (Step 6-9)，请确保远程 Ollama 已就绪"
        info "所需模型: qwen2.5:7b-instruct, nomic-embed-text"

        # Write to .env
        ENV_FILE="${ROOT_DIR}/LLM+RAG证据工程/.env"
        if [[ -f "${ENV_FILE}" ]]; then
            if grep -q "^OLLAMA_HOST=" "${ENV_FILE}"; then
                sed -i.bak "s|^OLLAMA_HOST=.*|OLLAMA_HOST=${REMOTE_OLLAMA}|" "${ENV_FILE}"
                rm -f "${ENV_FILE}.bak"
            else
                echo "OLLAMA_HOST=${REMOTE_OLLAMA}" >> "${ENV_FILE}"
            fi
            ok "已写入 OLLAMA_HOST=${REMOTE_OLLAMA} 到 .env"
        else
            info ".env 文件不存在，将在 start.sh setup 时创建"
        fi
    else
        info "跳过 Ollama 安装 (--skip-ollama)"
    fi
else
    if command -v ollama &>/dev/null; then
        ok "ollama: $(ollama --version 2>/dev/null | head -1 || echo 'installed')"
    else
        warn "ollama: not found"
        if confirm "安装 Ollama？"; then
            info "Installing Ollama..."
            if [[ "${OS_TYPE}" == "macos" ]]; then
                if command -v brew &>/dev/null; then
                    run_or_dry brew install ollama
                else
                    fail "macOS 需要 Homebrew 来安装 Ollama，或手动下载: https://ollama.com/download"
                    ISSUES+=("ollama install needs brew on macOS")
                fi
            else
                # Linux: official install script
                run_or_dry bash -c "curl -fsSL https://ollama.com/install.sh | sh"
            fi

            if command -v ollama &>/dev/null; then
                ok "Ollama 安装成功"
                INSTALLED+=("ollama")
            else
                fail "Ollama 安装失败"
                ISSUES+=("ollama install failed")
            fi
        fi
    fi

    # Ensure Ollama is running
    if command -v ollama &>/dev/null; then
        if curl -sf http://localhost:11434/api/tags &>/dev/null; then
            ok "Ollama 服务已运行"
        else
            info "启动 Ollama 服务..."
            if [[ "${OS_TYPE}" == "macos" ]]; then
                run_or_dry ollama serve &>/dev/null &
            else
                # Linux: try systemd first, then foreground
                if command -v systemctl &>/dev/null; then
                    run_or_dry ${SUDO} systemctl start ollama 2>/dev/null ||
                    run_or_dry ${SUDO} systemctl enable --now ollama 2>/dev/null ||
                    run_or_dry ollama serve &>/dev/null &
                else
                    run_or_dry ollama serve &>/dev/null &
                fi
            fi
            # Wait for startup
            for i in $(seq 1 15); do
                if curl -sf http://localhost:11434/api/tags &>/dev/null; then
                    ok "Ollama 服务已启动"
                    break
                fi
                sleep 1
            done
        fi

        # Pull required models
        LLM_MODEL="qwen2.5:7b-instruct"
        EMBED_MODEL="nomic-embed-text"

        for model in "${LLM_MODEL}" "${EMBED_MODEL}"; do
            if ollama list 2>/dev/null | grep -q "${model}"; then
                ok "Model ready: ${model}"
            else
                info "Pulling model: ${model} ..."
                if [[ "${DRY_RUN}" -eq 0 ]]; then
                    if ollama pull "${model}"; then
                        ok "Model pulled: ${model}"
                        INSTALLED+=("model:${model}")
                    else
                        fail "Failed to pull: ${model}"
                        ISSUES+=("ollama pull ${model} failed")
                    fi
                else
                    info "[dry-run] ollama pull ${model}"
                fi
            fi
        done
    fi
fi

# ═══════════════════════════════════════════════════════════════════════
# 4. ARCHS4 H5 数据文件 (44GB)
# ═══════════════════════════════════════════════════════════════════════

step "4. ARCHS4 H5 Data (Direction A 需要)"

ARCHS4_DIR="${ROOT_DIR}/archs4_signature_pipeline/data/archs4"

if [[ "${SKIP_ARCHS4}" -eq 1 ]]; then
    info "跳过 ARCHS4 H5 下载 (--skip-archs4)"
    info "如果只跑 Direction B (origin_only)，可以不需要这个文件"
else
    # Check for existing H5 files
    H5_FOUND=0
    if [[ -d "${ARCHS4_DIR}" ]]; then
        for f in "${ARCHS4_DIR}"/human_gene_v2.*.h5; do
            if [[ -f "$f" ]]; then
                SIZE_GB=$(du -g "$f" 2>/dev/null | awk '{print $1}' || echo "?")
                ok "Found: $(basename "$f") (${SIZE_GB} GB)"
                H5_FOUND=1
                break
            fi
        done
    fi

    if [[ "${H5_FOUND}" -eq 0 ]]; then
        warn "ARCHS4 H5 文件不存在"
        echo ""
        info "ARCHS4 H5 文件约 44GB，需要手动下载："
        echo ""
        printf "    ${BOLD}mkdir -p %s${NC}\n" "${ARCHS4_DIR}"
        printf "    ${BOLD}wget -c -O %s/human_gene_v2.5.h5 \\\\${NC}\n" "${ARCHS4_DIR}"
        printf "    ${BOLD}    'https://s3.dev.maayanlab.cloud/archs4/files/human_gene_v2.5.h5'${NC}\n"
        echo ""

        if confirm "现在开始下载？(44GB，需要稳定网络)"; then
            mkdir -p "${ARCHS4_DIR}"
            H5_URL="https://s3.dev.maayanlab.cloud/archs4/files/human_gene_v2.5.h5"
            H5_PATH="${ARCHS4_DIR}/human_gene_v2.5.h5"
            info "Downloading ARCHS4 H5 to ${H5_PATH} ..."
            if run_or_dry wget -c -q --show-progress -O "${H5_PATH}" "${H5_URL}"; then
                ok "ARCHS4 H5 下载完成"
                INSTALLED+=("archs4_h5")
            else
                fail "ARCHS4 H5 下载失败（可用 wget -c 断点续传重试）"
                ISSUES+=("archs4 h5 download failed")
            fi
        else
            warn "跳过下载 — Direction A (cross) 将无法使用 ARCHS4 签名"
        fi
    fi
fi

# ═══════════════════════════════════════════════════════════════════════
# 5. Directory permissions + runtime dirs
# ═══════════════════════════════════════════════════════════════════════

step "5. Directories & Permissions"

for dir in \
    "${ROOT_DIR}/runtime/state" \
    "${ROOT_DIR}/runtime/runs" \
    "${ROOT_DIR}/logs/pipeline" \
    "${ROOT_DIR}/data"; do
    if [[ ! -d "${dir}" ]]; then
        run_or_dry mkdir -p "${dir}"
        ok "Created: ${dir}"
    fi
    if [[ -w "${dir}" ]]; then
        ok "Writable: ${dir}"
    else
        fail "NOT writable: ${dir}"
        ISSUES+=("${dir} not writable")
    fi
done

# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════

header "Bootstrap Summary"

if [[ ${#INSTALLED[@]} -gt 0 ]]; then
    info "Installed:"
    for item in "${INSTALLED[@]}"; do
        ok "${item}"
    done
    echo ""
fi

if [[ ${#ISSUES[@]} -gt 0 ]]; then
    fail "Issues (${#ISSUES[@]}):"
    for issue in "${ISSUES[@]}"; do
        fail "  ${issue}"
    done
    echo ""
    fail "请修复以上问题后重新运行 bootstrap.sh"
    exit 1
fi

ok "Phase 0 完成! 所有前置条件就绪"
echo ""
info "Next steps:"
echo ""
printf "    ${BOLD}bash ops/start.sh setup${NC}    # Phase 1-2: 创建 venv, 安装 pip/R 依赖\n"
printf "    ${BOLD}bash ops/start.sh check${NC}    # 验证全部环境就绪\n"
printf "    ${BOLD}bash ops/start.sh run atherosclerosis${NC}  # 试跑一个疾病\n"
echo ""
