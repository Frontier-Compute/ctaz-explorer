#!/bin/bash
# pre-ship smoke test for ctaz-explorer
# usage: ./smoke.sh [base_url]
# defaults to http://127.0.0.1:8088

set -e
BASE=${1:-http://127.0.0.1:8088}
pass=0
fail=0

check() {
  local name=$1
  local path=$2
  local expect=$3
  local code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")
  if [ "$code" = "$expect" ]; then
    echo "PASS $name $path $code"
    pass=$((pass + 1))
  else
    echo "FAIL $name $path expected=$expect got=$code"
    fail=$((fail + 1))
  fi
}

check health / 200 # sanity check
check health /health 200
check home / 200
check finalizers /finalizers 200
check stake /stake 200
check block_by_height /block/100 200
check bogus_block /block/deadbeef 404
check bogus_tx /tx/deadbeef 404
check empty_search /search 307
check no_route /nope 404

echo
echo "pass: $pass  fail: $fail"
[ $fail -eq 0 ]
