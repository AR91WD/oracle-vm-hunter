#!/bin/bash
# Oracle A1.Flex PROGRESSIVE slot hunter.
# Strategy: grab ANY A1 capacity (biggest that fits, stepping down to 1 OCPU),
# then GROW the existing instance upward toward TARGET_OCPU/TARGET_GB via resize.
# One machine, growing 1→2→3→4. Single source of truth for GitHub + Mac.
#
# Required env: OCI_USER OCI_TENANCY OCI_FINGERPRINT KEY_FILE OCI_REGION SUBNET IMAGE SSH_KEY
# Optional env: AD (focus one AD; else cycle all 3) TG_TOKEN TG_CHAT_ID
#               DURATION (sec, default 240) TARGET_OCPU (default 4) SOURCE (label for logs)
set +e

TARGET_OCPU="${TARGET_OCPU:-4}"
GB_PER_OCPU=6
DURATION="${DURATION:-240}"
KEY_FILE="${KEY_FILE:-/tmp/oci-key.pem}"
SOURCE="${SOURCE:-worker}"
COMPARTMENT="$OCI_TENANCY"
API_HOST="iaas.${OCI_REGION}.oraclecloud.com"
INST_PATH="/20160918/instances"
DISPLAY_NAME="ai-hub"

if [ -n "$AD" ]; then ADS=("$AD"); else ADS=("gukW:EU-FRANKFURT-1-AD-1" "gukW:EU-FRANKFURT-1-AD-2" "gukW:EU-FRANKFURT-1-AD-3"); fi

b64() { openssl enc -base64 | tr -d '\n'; }

tg() {
  [ -z "$TG_CHAT_ID" ] && return
  local i
  for i in 1 2 3; do
    curl -s --max-time 10 -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      -d "chat_id=${TG_CHAT_ID}" -d "text=$1" 2>&1 | grep -q '"ok":true' && return 0
    sleep 3
  done
}

# Send an alert at most once per hour per key (kills spam during flaky internet)
STATE_DIR="${STATE_DIR:-$HOME/.vmhunter-state}"
mkdir -p "$STATE_DIR" 2>/dev/null
alert_dedup() {
  local key="$1" msg="$2" f="${STATE_DIR}/alert-${key}" now
  now=$(date +%s)
  if [ -f "$f" ]; then
    local last; last=$(cat "$f" 2>/dev/null); [ -z "$last" ] && last=0
    [ $((now - last)) -lt 3600 ] && return
  fi
  echo "$now" > "$f"
  tg "$msg"
}

# sign_send METHOD PATH [BODY]
sign_send() {
  local method="$1" path="$2" body="$3"
  local now key_id sig_string signature M
  now=$(date -u '+%a, %d %b %Y %H:%M:%S GMT')
  key_id="${OCI_TENANCY}/${OCI_USER}/${OCI_FINGERPRINT}"
  M=$(echo "$method" | tr '[:lower:]' '[:upper:]')
  if [ "$method" = "get" ]; then
    sig_string="(request-target): get ${path}
date: ${now}
host: ${API_HOST}"
    signature=$(printf '%s' "$sig_string" | openssl dgst -sha256 -sign "$KEY_FILE" | b64)
    curl -s --max-time 15 -X GET "https://${API_HOST}${path}" \
      -H "date: ${now}" -H "host: ${API_HOST}" \
      -H "authorization: Signature version=\"1\",keyId=\"${key_id}\",algorithm=\"rsa-sha256\",headers=\"(request-target) date host\",signature=\"${signature}\"" 2>&1
  else
    local sha len
    sha=$(printf '%s' "$body" | openssl dgst -sha256 -binary | b64)
    len=${#body}
    sig_string="(request-target): ${method} ${path}
date: ${now}
host: ${API_HOST}
content-length: ${len}
content-type: application/json
x-content-sha256: ${sha}"
    signature=$(printf '%s' "$sig_string" | openssl dgst -sha256 -sign "$KEY_FILE" | b64)
    curl -s --max-time 15 -X "$M" "https://${API_HOST}${path}" \
      -H "date: ${now}" -H "host: ${API_HOST}" \
      -H "content-type: application/json" -H "content-length: ${len}" \
      -H "x-content-sha256: ${sha}" \
      -H "authorization: Signature version=\"1\",keyId=\"${key_id}\",algorithm=\"rsa-sha256\",headers=\"(request-target) date host content-length content-type x-content-sha256\",signature=\"${signature}\"" \
      -d "$body" 2>&1
  fi
}

create_body() {
  local ad="$1" oc="$2" gb="$3"
  printf '{"compartmentId":"%s","availabilityDomain":"%s","displayName":"%s","shape":"VM.Standard.A1.Flex","shapeConfig":{"ocpus":%s,"memoryInGBs":%s},"imageId":"%s","createVnicDetails":{"subnetId":"%s","assignPublicIp":true},"sourceDetails":{"sourceType":"image","imageId":"%s","bootVolumeSizeInGBs":100},"metadata":{"ssh_authorized_keys":"%s"}}' \
    "$COMPARTMENT" "$ad" "$DISPLAY_NAME" "$oc" "$gb" "$IMAGE" "$SUBNET" "$IMAGE" "$SSH_KEY"
}

resize_body() {
  local oc="$1" gb="$2"
  printf '{"shapeConfig":{"ocpus":%s,"memoryInGBs":%s}}' "$oc" "$gb"
}

# Returns "USED_OCPU|PRIMARY_ID|PRIMARY_OCPU"
survey() {
  local resp
  resp=$(sign_send get "${INST_PATH}?compartmentId=${COMPARTMENT}")
  printf '%s' "$resp" | python3 -c '
import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    print("0||0"); sys.exit()
if not isinstance(d,list): d=[]
tot=0; pid=""; poc=0
# prefer ai-hub named instance as the one we grow
cand=[]
for it in d:
    if it.get("lifecycleState","") in ("TERMINATED","TERMINATING"): continue
    if "A1" not in it.get("shape",""): continue
    oc=(it.get("shapeConfig") or {}).get("ocpus",0) or 0
    tot+=oc
    cand.append((it.get("displayName",""), it.get("id",""), oc))
if cand:
    named=[c for c in cand if c[0]=="ai-hub"]
    pick=named[0] if named else cand[0]
    pid=pick[1]; poc=pick[2]
print(f"{int(tot)}|{pid}|{int(poc)}")
' 2>/dev/null
}

echo "[$SOURCE] survey capacity..."
S=$(survey)
USED=${S%%|*}; rest=${S#*|}; PID=${rest%%|*}; POC=${rest#*|}
[ -z "$USED" ] && USED=0
[ -z "$POC" ] && POC=0
echo "[$SOURCE] used=${USED} OCPU, primary=${PID:-none} (${POC} OCPU), target=${TARGET_OCPU}"

# Already at/above target → done
if [ "$USED" -ge "$TARGET_OCPU" ] 2>/dev/null; then
  echo "[$SOURCE] TARGET REACHED (${USED} OCPU). Disabling hunt."
  if [ -n "$GH_TOKEN" ] && [ -n "$GH_REPO" ]; then
    curl -s -X PUT -H "Authorization: token $GH_TOKEN" -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/$GH_REPO/actions/workflows/retry-vm.yml/disable" >/dev/null 2>&1
  fi
  exit 0
fi

if [ "$USED" -eq 0 ]; then MODE="create"; else MODE="grow"; fi
echo "[$SOURCE] mode=${MODE}"

END=$(($(date +%s) + DURATION))
size=$TARGET_OCPU
sleep_between=8
rate=0
ad_i=0

while [ $(date +%s) -lt $END ]; do
  # pick AD (rotate if multiple)
  ad="${ADS[$((ad_i % ${#ADS[@]}))]}"
  ad_i=$((ad_i + 1))
  ad_short="${ad##*-}"
  gb=$((size * GB_PER_OCPU))

  if [ "$MODE" = "create" ]; then
    [ "$size" -lt 1 ] && size=$TARGET_OCPU
    echo -n "[$(date -u '+%H:%M:%S') $SOURCE] CREATE ${size}oc/${gb}gb ${ad_short}: "
    result=$(sign_send post "$INST_PATH" "$(create_body "$ad" "$size" "$gb")")
  else
    # grow existing primary; only sizes strictly greater than current primary
    local_min=$((POC + 1))
    [ "$size" -lt "$local_min" ] && size=$TARGET_OCPU
    echo -n "[$(date -u '+%H:%M:%S') $SOURCE] GROW ${POC}→${size}oc ${ad_short}: "
    if [ -z "$PID" ]; then echo "no primary, re-survey"; break; fi
    result=$(sign_send put "${INST_PATH}/${PID}" "$(resize_body "$size" "$gb")")
  fi

  # TRANSIENT network/transport failure (flaky internet): empty body, curl error,
  # HTML error page, or DNS failure. Do NOT alert — just wait for net and retry.
  if [ -z "$result" ] || echo "$result" | grep -qiE "curl:|Could not resolve|Connection (refused|reset|timed out)|Operation timed out|Failed to connect|<html|<!DOCTYPE|Empty reply|SSL|handshake"; then
    echo "net blip / transient — waiting"
    # brief wait for connectivity to recover
    n=0
    while ! curl -sf --max-time 5 https://oracle.com >/dev/null 2>&1; do
      n=$((n+1)); [ "$n" -ge 30 ] && break; sleep 5
    done
    sleep 3
    continue
  fi

  # SUCCESS (create → PROVISIONING/RUNNING; grow → returns instance with shapeConfig)
  if echo "$result" | grep -q '"lifecycleState"'; then
    st=$(echo "$result" | grep -o '"lifecycleState"[^,]*' | head -1 | cut -d'"' -f4)
    newoc=$(echo "$result" | grep -o '"ocpus":[0-9.]*' | head -1 | cut -d: -f2 | cut -d. -f1)
    [ -z "$newoc" ] && newoc=$size
    if [ "$st" != "TERMINATED" ] && [ "$st" != "TERMINATING" ]; then
      vm_id=$(echo "$result" | grep -o '"id":"ocid1.instance[^"]*"' | head -1 | cut -d'"' -f4)
      echo "OK ${st} now ${newoc} OCPU"
      if [ "$newoc" -ge "$TARGET_OCPU" ] 2>/dev/null; then
        tg "🎉 ПОЗДРАВЛЯЮ! Полный сервер 4 OCPU/24GB готов!%0AAD:${ad_short} State:${st}%0AOCID:${vm_id}%0AConsole: https://cloud.oracle.com/compute/instances"
        mkdir -p "$HOME/.vmhunter-state" 2>/dev/null; touch "$HOME/.vmhunter-state/vm-caught" 2>/dev/null
        [ -n "$GH_TOKEN" ] && curl -s -X PUT -H "Authorization: token $GH_TOKEN" -H "Accept: application/vnd.github+json" \
          "https://api.github.com/repos/$GH_REPO/actions/workflows/retry-vm.yml/disable" >/dev/null 2>&1
        exit 0
      else
        tg "✅ Прогресс! Сервер теперь ${newoc} OCPU (цель ${TARGET_OCPU}). Продолжаю растить.%0AOCID:${vm_id}"
        # keep hunting to grow further; re-survey next run
        exit 0
      fi
    fi
  fi

  # OUT OF CAPACITY → step size down (try smaller chunk)
  if echo "$result" | grep -qiE "Out of host capacity|OutOfCapacity|InternalError"; then
    echo "no capacity @${size}oc"
    size=$((size - 1))
    if [ "$MODE" = "create" ] && [ "$size" -lt 1 ]; then size=$TARGET_OCPU; fi
    if [ "$MODE" = "grow" ] && [ "$size" -le "$POC" ]; then size=$TARGET_OCPU; fi
    sleep "$sleep_between"
    continue
  fi

  # LIMIT: already have max for this size — in grow mode means resize wouldn't fit; step down
  if echo "$result" | grep -qiE "LimitExceeded|QuotaExceeded"; then
    echo "limit @${size}oc"
    size=$((size - 1))
    if [ "$MODE" = "create" ] && [ "$size" -lt 1 ]; then
      echo "[$SOURCE] full capacity used, re-survey next run"; exit 0
    fi
    if [ "$MODE" = "grow" ] && [ "$size" -le "$POC" ]; then size=$TARGET_OCPU; fi
    sleep "$sleep_between"
    continue
  fi

  # RATE LIMIT
  if echo "$result" | grep -q "TooManyRequests"; then
    rate=$((rate + 1))
    [ "$sleep_between" -lt 15 ] && sleep_between=$((sleep_between + 1))
    echo "rate-limited (sleep→${sleep_between}s)"
    if [ "$rate" -ge 12 ]; then sleep 120; rate=0; else sleep 60; fi
    continue
  fi

  # AUTH FAIL — degrade, alert MAX 1/hour, keep trying
  if echo "$result" | grep -qiE "NotAuthenticated|Authentication failed|InvalidSignature|Failed to verify"; then
    echo "AUTH FAIL"
    alert_dedup "auth" "🚨 ${SOURCE}: OCI ключ не проходит авторизацию. Проверь OCI_KEY. Продолжаю стучать (вдруг временный сбой)."
    sleep 30
    continue
  fi

  # UNKNOWN — possible API change, alert MAX 1/hour
  short=$(echo "$result" | head -c 150 | tr '\n' ' ')
  echo "UNKNOWN: $short"
  alert_dedup "unknown" "⚠️ ${SOURCE}: странный ответ Oracle (возможно API изменился):%0A${short}%0AПродолжаю старым методом."
  sleep "$sleep_between"
done

echo "[$SOURCE] run done"
