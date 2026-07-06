#!/usr/bin/env bash
# Default sibling checkout paths under UNIFY_STACK_ROOT.

default_unity_repo_path() {
  local stack_root="${1:-${UNIFY_STACK_ROOT:-}}"
  if [[ -z "$stack_root" ]]; then
    return 1
  fi
  if [[ -d "$stack_root/unify" ]]; then
    printf '%s' "$stack_root/unify"
  elif [[ -d "$stack_root/unity" ]]; then
    printf '%s' "$stack_root/unity"
  else
    printf '%s' "$stack_root/unify"
  fi
}
