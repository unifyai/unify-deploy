#!/usr/bin/env bash
# =============================================================================
# prompt_byok_keys.sh — Interactive BYOK wizard for local / self-host installs
# =============================================================================
#
# Prompts for keys missing from unity/.env. Idempotent: skips keys already set.
# Voice keys (Deepgram + Cartesia) are prompted by default — voice is core.
#
# Usage:
#   UNITY_REPO=/path/to/unity ./scripts/prompt_byok_keys.sh
#   ./scripts/prompt_byok_keys.sh --non-interactive   # skip prompts (CI)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

_looks_like_unity_repo() {
  local dir="$1"
  [[ -d "$dir" && -f "$dir/pyproject.toml" && -d "$dir/unity" ]]
}

resolve_unity_repo() {
  local candidate=""
  if [[ -n "${UNITY_REPO:-}" && -d "$UNITY_REPO" ]]; then
    printf '%s' "$UNITY_REPO"
    return 0
  fi
  if [[ -n "${UNITY_REPO_PATH:-}" && -d "$UNITY_REPO_PATH" ]]; then
    printf '%s' "$UNITY_REPO_PATH"
    return 0
  fi
  candidate="$(cd "$SCRIPT_DIR/.." && pwd -P)"
  if _looks_like_unity_repo "$candidate"; then
    printf '%s' "$candidate"
    return 0
  fi
  candidate="$(pwd -P)"
  if _looks_like_unity_repo "$candidate"; then
    printf '%s' "$candidate"
    return 0
  fi
  if [[ -n "${UNIFY_STACK_ROOT:-}" && -d "$UNIFY_STACK_ROOT/unity" ]]; then
    printf '%s' "$UNIFY_STACK_ROOT/unity"
    return 0
  fi
  printf '%s' "${UNITY_HOME:-$HOME/.unity}/unity"
}

UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
UNITY_REPO="$(resolve_unity_repo)"
if _looks_like_unity_repo "$UNITY_REPO"; then
  UNITY_HOME="$(cd "$UNITY_REPO/.." && pwd -P)"
fi
ENV_FILE="${UNITY_ENV_FILE:-$UNITY_REPO/.env}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'
log_info()    { echo -e "${CYAN}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }

NON_INTERACTIVE="${NON_INTERACTIVE:-false}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    *) shift ;;
  esac
done

has_env_value() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] && grep -qE "^${key}=.+$" "$ENV_FILE"
}

read_env_value() {
  local key="$1"
  if [[ ! -f "$ENV_FILE" ]]; then
    return 1
  fi
  grep -E "^${key}=" "$ENV_FILE" | head -1 | sed 's/^[^=]*=//'
}

upsert_env() {
  local key="$1"
  local val="$2"
  val="${val//$'\n'/}"
  val="${val//$'\r'/}"
  if [[ ! -f "$ENV_FILE" ]]; then
    touch "$ENV_FILE"
  fi
  if grep -qE "^${key}=" "$ENV_FILE"; then
    python3 - "$ENV_FILE" "$key" "$val" <<'PYEOF'
import sys, re
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    lines = f.readlines()
pat = re.compile(rf'^{re.escape(key)}=')
for i, line in enumerate(lines):
    if pat.match(line):
        lines[i] = f'{key}={val}\n'
        break
else:
    lines.append(f'{key}={val}\n')
with open(path, 'w') as f:
    f.writelines(lines)
PYEOF
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

prompt_secret() {
  local label="$1"
  local var_name="$2"
  local hint="$3"
  local value=""

  if has_env_value "$var_name"; then
    log_success "$var_name already set"
    return 0
  fi

  if [[ "$NON_INTERACTIVE" == "true" ]] || [[ ! -r /dev/tty ]] || [[ ! -w /dev/tty ]]; then
    log_warn "$var_name not set — add it to $ENV_FILE"
    log_info "  $hint"
    return 0
  fi

  echo "" >/dev/tty
  echo -e "${BOLD}$label${NC}" >/dev/tty
  echo "  $hint" >/dev/tty
  printf "Paste %s (hidden, Enter to skip): " "$var_name" >/dev/tty
  if ! IFS= read -rs value </dev/tty 2>/dev/null; then
    IFS= read -r value </dev/tty || value=""
  fi
  echo "" >/dev/tty

  if [[ -z "$value" ]]; then
    log_warn "Skipped $var_name"
    return 0
  fi

  upsert_env "$var_name" "$value"
  log_success "Wrote $var_name to $ENV_FILE"
}

prompt_llm_key() {
  if has_env_value OPENAI_API_KEY \
    || has_env_value ANTHROPIC_API_KEY \
    || has_env_value DEEPSEEK_API_KEY; then
    log_success "LLM provider key already set"
    return 0
  fi

  if [[ "$NON_INTERACTIVE" == "true" ]] || [[ ! -r /dev/tty ]] || [[ ! -w /dev/tty ]]; then
    log_warn "No LLM key set — add OPENAI_API_KEY, ANTHROPIC_API_KEY, or DEEPSEEK_API_KEY to $ENV_FILE"
    return 0
  fi

  echo "" >/dev/tty
  echo -e "${BOLD}LLM provider (required for chat)${NC}" >/dev/tty
  echo "  1) OpenAI    — https://platform.openai.com/api-keys" >/dev/tty
  echo "  2) Anthropic — https://console.anthropic.com/" >/dev/tty
  echo "  3) DeepSeek  — https://platform.deepseek.com" >/dev/tty
  echo "  4) Skip" >/dev/tty
  local choice=""
  printf "Choice [1-4, default 1]: " >/dev/tty
  IFS= read -r choice </dev/tty || choice=""
  choice="${choice:-1}"

  local var_name=""
  case "$choice" in
    1) var_name="OPENAI_API_KEY" ;;
    2) var_name="ANTHROPIC_API_KEY" ;;
    3) var_name="DEEPSEEK_API_KEY" ;;
    *) log_warn "Skipped LLM key"; return 0 ;;
  esac

  prompt_secret "LLM" "$var_name" "Lets Marty think and reply — required for chat."
}

ensure_embedding_search_key() {
  if has_env_value OPENAI_API_KEY; then
    return 0
  fi
  if ! has_env_value ANTHROPIC_API_KEY && ! has_env_value DEEPSEEK_API_KEY; then
    return 0
  fi

  if [[ "$NON_INTERACTIVE" == "true" ]] || [[ ! -r /dev/tty ]] || [[ ! -w /dev/tty ]]; then
    log_warn "OPENAI_API_KEY not set — tool-search embeddings need OpenAI even when chat uses another provider"
    return 0
  fi

  echo "" >/dev/tty
  echo -e "${BOLD}OpenAI for tool search (recommended)${NC}" >/dev/tty
  echo "  Marty picks tools using OpenAI embeddings, even when chat runs on another provider." >/dev/tty
  echo "  Add an OpenAI key so Marty can reach its desktop, browser, and research tools." >/dev/tty
  prompt_secret \
    "OpenAI embeddings" \
    "OPENAI_API_KEY" \
    "https://platform.openai.com/api-keys"
}

ensure_default_chat_model() {
  if has_env_value UNIFY_MODEL; then
    return 0
  fi
  # Compose passes UNIFY_MODEL through to the runtime even when blank, which
  # would override Unity's built-in default — so pin a model that matches the
  # chat key the user actually provided.
  local model=""
  if has_env_value DEEPSEEK_API_KEY; then
    model="deepseek-v4-max@deepseek"
  elif has_env_value ANTHROPIC_API_KEY; then
    model="claude-4.6-sonnet@anthropic"
  elif has_env_value OPENAI_API_KEY; then
    model="gpt-5.4@openai"
  else
    return 0
  fi
  upsert_env "UNIFY_MODEL" "$model"
  log_success "Set UNIFY_MODEL=$model (edit in $ENV_FILE to change)"
}

sync_anticaptcha_keys() {
  local key=""
  if has_env_value ANTICAPTCHA_KEY; then
    key="$(read_env_value ANTICAPTCHA_KEY)"
  elif has_env_value UNITY_ACTOR_ANTICAPTCHA_KEY; then
    key="$(read_env_value UNITY_ACTOR_ANTICAPTCHA_KEY)"
  fi
  if [[ -z "$key" ]]; then
    return 0
  fi
  if ! has_env_value ANTICAPTCHA_KEY; then
    upsert_env "ANTICAPTCHA_KEY" "$key"
    log_success "Mirrored ANTICAPTCHA_KEY for agent-service"
  fi
  if ! has_env_value UNITY_ACTOR_ANTICAPTCHA_KEY; then
    upsert_env "UNITY_ACTOR_ANTICAPTCHA_KEY" "$key"
    log_success "Mirrored UNITY_ACTOR_ANTICAPTCHA_KEY for Unity CM"
  fi
}

prompt_anticaptcha_key() {
  sync_anticaptcha_keys
  if has_env_value ANTICAPTCHA_KEY || has_env_value UNITY_ACTOR_ANTICAPTCHA_KEY; then
    log_success "AntiCaptcha key already set"
    sync_anticaptcha_keys
    return 0
  fi

  if [[ "$NON_INTERACTIVE" == "true" ]] || [[ ! -r /dev/tty ]] || [[ ! -w /dev/tty ]]; then
    log_warn "AntiCaptcha not set — add ANTICAPTCHA_KEY to $ENV_FILE for computer-use CAPTCHA solving"
    return 0
  fi

  local value=""
  echo "" >/dev/tty
  echo -e "${BOLD}AntiCaptcha (optional — computer use)${NC}" >/dev/tty
  echo "  Lets Marty get past CAPTCHAs while driving the browser/desktop instead of stalling." >/dev/tty
  echo "  Not needed for chat or voice-only installs." >/dev/tty
  echo "  Sign up: https://anti-captcha.com" >/dev/tty
  printf "Paste ANTICAPTCHA_KEY (hidden, Enter to skip): " >/dev/tty
  if ! IFS= read -rs value </dev/tty 2>/dev/null; then
    IFS= read -r value </dev/tty || value=""
  fi
  echo "" >/dev/tty

  if [[ -z "$value" ]]; then
    log_warn "Skipped AntiCaptcha"
    return 0
  fi

  upsert_env "ANTICAPTCHA_KEY" "$value"
  upsert_env "UNITY_ACTOR_ANTICAPTCHA_KEY" "$value"
  log_success "Wrote ANTICAPTCHA_KEY + UNITY_ACTOR_ANTICAPTCHA_KEY to $ENV_FILE"
}

prompt_research_and_computer() {
  echo ""
  echo -e "${BOLD}Research + computer automation (optional)${NC}"
  echo "  Tavily:      lets Marty look things up on the web while researching"
  echo "  AntiCaptcha: lets Marty get past CAPTCHAs while using the browser/desktop"
  echo ""

  prompt_secret \
    "Web search — Tavily API key" \
    "UNITY_WEB_TAVILY_API_KEY" \
    "Lets Marty search the web while researching. Free tier: https://tavily.com"

  if has_env_value UNITY_WEB_TAVILY_API_KEY && ! has_env_value UNITY_WEB_ENABLED; then
    upsert_env "UNITY_WEB_ENABLED" "true"
    log_success "Set UNITY_WEB_ENABLED=true (web search on)"
  elif has_env_value UNITY_WEB_TAVILY_API_KEY; then
    log_success "UNITY_WEB_ENABLED already set"
  fi

  prompt_anticaptcha_key

  echo ""
  if has_env_value UNITY_WEB_TAVILY_API_KEY; then
    log_success "Web search (Tavily) configured"
  else
    log_warn "Web search skipped — Coordinator research tools stay disabled"
  fi
  if has_env_value ANTICAPTCHA_KEY || has_env_value UNITY_ACTOR_ANTICAPTCHA_KEY; then
    log_success "AntiCaptcha configured for computer automation"
  else
    log_info "AntiCaptcha not set (optional until computer use)"
  fi
  echo ""
}

prompt_app_integrations() {
  echo ""
  echo -e "${BOLD}App integrations (optional)${NC}"
  echo "  Lets Marty act in third-party apps (HubSpot, Notion, GitHub, ...) on your"
  echo "  behalf, using your own Composio account."
  echo ""

  prompt_secret \
    "App integrations — Composio API key" \
    "COMPOSIO_API_KEY" \
    "Lets Marty act in apps like Notion, GitHub, and HubSpot. Free tier: https://composio.dev"

  echo ""
  if has_env_value COMPOSIO_API_KEY; then
    log_success "Composio app integrations configured"
  else
    log_warn "Composio skipped — third-party app integrations stay disabled"
  fi
  echo ""
}

ensure_voice_provider_from_keys() {
  if has_env_value VOICE_PROVIDER; then
    return 0
  fi
  if has_env_value ELEVEN_API_KEY; then
    upsert_env "VOICE_PROVIDER" "elevenlabs"
    log_success "Set VOICE_PROVIDER=elevenlabs"
  elif has_env_value CARTESIA_API_KEY; then
    upsert_env "VOICE_PROVIDER" "cartesia"
    log_success "Set VOICE_PROVIDER=cartesia"
  fi
}

prompt_tts_provider() {
  if has_env_value CARTESIA_API_KEY || has_env_value ELEVEN_API_KEY; then
    ensure_voice_provider_from_keys
    log_success "Text-to-speech key already set"
    return 0
  fi

  if [[ "$NON_INTERACTIVE" == "true" ]] || [[ ! -r /dev/tty ]] || [[ ! -w /dev/tty ]]; then
    log_warn "No TTS key set — add CARTESIA_API_KEY or ELEVEN_API_KEY to $ENV_FILE for browser calls"
    return 0
  fi

  echo "" >/dev/tty
  echo -e "${BOLD}Text-to-speech (required for browser calls)${NC}" >/dev/tty
  echo "  Picks the voice Marty speaks back with on calls." >/dev/tty
  echo "  1) Cartesia   — https://play.cartesia.ai (free credits)" >/dev/tty
  echo "  2) ElevenLabs — https://elevenlabs.io (free credits)" >/dev/tty
  echo "  3) Skip" >/dev/tty
  local choice=""
  printf "Choice [1-3, default 1]: " >/dev/tty
  IFS= read -r choice </dev/tty || choice=""
  choice="${choice:-1}"

  case "$choice" in
    1) prompt_secret "Cartesia (text-to-speech)" "CARTESIA_API_KEY" "Lets Marty speak back on calls. Free credits: https://play.cartesia.ai" ;;
    2) prompt_secret "ElevenLabs (text-to-speech)" "ELEVEN_API_KEY" "Lets Marty speak back on calls. Free credits: https://elevenlabs.io" ;;
    *) log_warn "Skipped text-to-speech"; return 0 ;;
  esac

  ensure_voice_provider_from_keys
}

import_shell_env_keys() {
  local key val
  for key in OPENAI_API_KEY ANTHROPIC_API_KEY DEEPSEEK_API_KEY DEEPGRAM_API_KEY \
    CARTESIA_API_KEY ELEVEN_API_KEY VOICE_PROVIDER UNIFY_MODEL \
    UNITY_WEB_TAVILY_API_KEY ANTICAPTCHA_KEY COMPOSIO_API_KEY; do
    val="${!key:-}"
    [[ -z "$val" ]] && continue
    if ! has_env_value "$key"; then
      upsert_env "$key" "$val"
      log_success "Imported $key from environment into $ENV_FILE"
    fi
  done
}

mark_byok_configured() {
  upsert_env "UNITY_BYOK_CONFIGURED" "1"
}

run_non_interactive_byok() {
  import_shell_env_keys
  prompt_llm_key
  ensure_embedding_search_key
  ensure_default_chat_model
  prompt_secret \
    "Speech-to-text (required for browser calls)" \
    "DEEPGRAM_API_KEY" \
    "Lets Marty hear you on browser calls. Free tier: https://console.deepgram.com"
  prompt_tts_provider
  sync_anticaptcha_keys
  mark_byok_configured
  log_success "BYOK keys synced (non-interactive)"
}

compose_install_mode() {
  [[ "${UNITY_COMPOSE_INSTALL:-0}" == "1" && -f "$ENV_FILE" ]]
}

main() {
  if ! _looks_like_unity_repo "$UNITY_REPO"; then
    if compose_install_mode; then
      log_info "Compose install — configuring $ENV_FILE"
    else
      log_warn "Unity repo not found at $UNITY_REPO — skipping BYOK prompts"
      exit 0
    fi
  fi

  if [[ "$NON_INTERACTIVE" == "true" ]]; then
    run_non_interactive_byok
    exit 0
  fi

  if [[ "${UNITY_BYOK_FORCE:-0}" != "1" ]] && has_env_value UNITY_BYOK_CONFIGURED; then
    import_shell_env_keys
    ensure_default_chat_model
    sync_anticaptcha_keys
    log_success "BYOK already configured in $ENV_FILE — skipping wizard"
    log_info "Set UNITY_BYOK_FORCE=1 to run the wizard again"
    exit 0
  fi

  echo ""
  echo -e "${BOLD}BYOK setup${NC} — provider keys for chat, voice, and tools"
  echo ""
  echo "  Required:  LLM key (OpenAI, Anthropic, or DeepSeek)"
  echo "  Voice:     Deepgram + Cartesia/ElevenLabs (browser calls)"
  echo "  Optional:  Tavily (web search), AntiCaptcha (computer use)"
  echo "  Optional:  Composio (third-party app integrations)"
  echo ""

  import_shell_env_keys
  prompt_llm_key
  ensure_embedding_search_key
  ensure_default_chat_model
  prompt_secret \
    "Speech-to-text (required for browser calls)" \
    "DEEPGRAM_API_KEY" \
    "Lets Marty hear you on browser calls. Free tier: https://console.deepgram.com"
  prompt_tts_provider

  echo ""
  if has_env_value DEEPGRAM_API_KEY && { has_env_value CARTESIA_API_KEY || has_env_value ELEVEN_API_KEY; }; then
    log_success "Voice BYOK keys configured"
  else
    log_warn "Voice calls need DEEPGRAM_API_KEY + a TTS key (CARTESIA_API_KEY or ELEVEN_API_KEY) in $ENV_FILE"
  fi

  prompt_research_and_computer
  prompt_app_integrations
  mark_byok_configured
}

main "$@"
