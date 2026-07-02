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

RUN_START=$(date +%s)
END=$((RUN_START + DURATION))
size=$TARGET_OCPU
rate=0
ad_i=0
attempt=0
min_seen=$size
max_seen=$size

# --- Smooth adaptive pacing (replaces burst + long dead-pause) ---
# Measured tenancy-wide budget: Oracle accepts ~1 launch/resize request per
# ~15s per tenancy (empirical: 491 accepted / 7200s over a 2h window). With
# 4 workers total (3 GH matrix + 1 Mac), per-worker spacing of ~60s puts the
# aggregate stream right at that ceiling with near-zero 429s and — critically —
# near-zero dead air. Old scheme (8-15s bursts then 60-120s pauses on 429)
# had the same average throughput but terrible worst-case gaps; a transient
# capacity window opening during a dead pause was unwatchable. Uniform spacing
# minimizes the max-gap between probes, which is what actually determines the
# chance of catching a short-lived slot.
PACE="${PACE_BASE:-60}"
PACE_MIN=40
PACE_MAX=90
clean_streak=0

pace_sleep() {
  # ±20% jitter keeps the 4 independent workers from drifting into sync
  # (synchronized probes collide on the same bucket token → clustered 429s).
  local j=$((PACE / 5))
  [ "$j" -lt 1 ] && j=1
  local s=$((PACE - j + RANDOM % (2 * j + 1)))
  sleep "$s"
}

# Size selection: alternate the SMALLEST viable size with a descending cycle of
# larger ones — [1,4,1,3,1,2] in create mode. Rationale: when capacity frees up
# it is usually a small crumb; 1 OCPU has by far the highest fit probability, so
# it gets 50% of probes (was 25% under the plain 4→3→2→1 cycle). The large
# sizes stay in rotation so a big block, if one opens, is still grabbed within
# ~2 probes. In grow mode same idea relative to current size: alternate the
# minimal next step (POC+1, most likely to fit) with the remaining larger sizes.
pick_size() {
  if [ "$MODE" = "create" ]; then
    if [ $((attempt % 2)) -eq 1 ]; then
      size=1
    else
      case $(( (attempt / 2) % 3 )) in
        1) size=$TARGET_OCPU ;;
        2) size=$((TARGET_OCPU - 1)) ;;
        0) size=$((TARGET_OCPU - 2)) ;;
      esac
    fi
  else
    local minv=$((POC + 1))
    if [ $((attempt % 2)) -eq 1 ]; then
      size=$minv
    else
      local span=$((TARGET_OCPU - minv))
      if [ "$span" -le 0 ]; then
        size=$minv
      else
        size=$((TARGET_OCPU - ( (attempt / 2) % span )))
      fi
    fi
  fi
  [ "$size" -gt "$TARGET_OCPU" ] && size=$TARGET_OCPU
  [ "$size" -lt 1 ] && size=1
}

# One-line compact JSON telemetry, emitted once at every run-ending exit point.
# Consumed downstream by retry-vm.yml's relaunch job (GH side) or retry-vm.sh (Mac side).
emit_stats() {
  local elapsed=$(( $(date +%s) - RUN_START ))
  [ "$elapsed" -lt 1 ] && elapsed=1
  echo "STATS $(date -u +%s) {\"source\":\"${SOURCE}\",\"ad\":\"${ad_short:-n/a}\",\"attempts\":${attempt},\"min_size\":${min_seen},\"max_size\":${max_seen},\"rate_limits\":${rate},\"elapsed\":${elapsed},\"pace\":${PACE}}"
}

while [ $(date +%s) -lt $END ]; do
  # pick AD (rotate if multiple)
  ad="${ADS[$((ad_i % ${#ADS[@]}))]}"
  ad_i=$((ad_i + 1))
  ad_short="${ad##*-}"
  attempt=$((attempt + 1))
  pick_size
  gb=$((size * GB_PER_OCPU))
  [ "$size" -lt "$min_seen" ] && min_seen=$size
  [ "$size" -gt "$max_seen" ] && max_seen=$size

  if [ "$MODE" = "create" ]; then
    echo -n "[$(TZ='Europe/Paris' date '+%H:%M:%S %Z') $SOURCE] CREATE ${size}oc/${gb}gb ${ad_short} (pace ${PACE}s): "
    result=$(sign_send post "$INST_PATH" "$(create_body "$ad" "$size" "$gb")")
  else
    echo -n "[$(TZ='Europe/Paris' date '+%H:%M:%S %Z') $SOURCE] GROW ${POC}→${size}oc ${ad_short} (pace ${PACE}s): "
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
        emit_stats
        exit 0
      else
        tg "✅ Прогресс! Сервер теперь ${newoc} OCPU (цель ${TARGET_OCPU}). Продолжаю растить.%0AOCID:${vm_id}"
        # keep hunting to grow further; re-survey next run
        emit_stats
        exit 0
      fi
    fi
  fi

  # OUT OF CAPACITY — a clean (accepted) response; sequence handles size variety.
  if echo "$result" | grep -qiE "Out of host capacity|OutOfCapacity|InternalError"; then
    echo "no capacity @${size}oc"
    clean_streak=$((clean_streak + 1))
    if [ "$clean_streak" -ge 6 ] && [ "$PACE" -gt "$PACE_MIN" ]; then
      PACE=$((PACE - 2)); clean_streak=0
    fi
    pace_sleep
    continue
  fi

  # LIMIT/QUOTA: at size 1 in create mode this means quota is actually occupied
  # (survey disagreed) — exit and let the next generation re-survey.
  if echo "$result" | grep -qiE "LimitExceeded|QuotaExceeded"; then
    echo "limit @${size}oc"
    if [ "$MODE" = "create" ] && [ "$size" -le 1 ]; then
      echo "[$SOURCE] quota occupied at 1oc — re-survey next run"; emit_stats; exit 0
    fi
    clean_streak=$((clean_streak + 1))
    pace_sleep
    continue
  fi

  # RATE LIMIT — no more long dead pauses: bump personal spacing and keep the
  # stream flowing. The adaptive PACE rides just under Oracle's refill rate;
  # a storm (12+ in one run) still gets one 120s cooldown as a safety valve.
  if echo "$result" | grep -q "TooManyRequests"; then
    rate=$((rate + 1))
    clean_streak=0
    [ "$PACE" -lt "$PACE_MAX" ] && PACE=$((PACE + 10))
    [ "$PACE" -gt "$PACE_MAX" ] && PACE=$PACE_MAX
    echo "rate-limited (pace→${PACE}s)"
    if [ $((rate % 12)) -eq 0 ]; then
      echo "[$SOURCE] 429 storm — 120s cooldown"
      sleep 120
    else
      pace_sleep
    fi
    continue
  fi

  # AUTH FAIL — degrade, alert MAX 1/hour, keep trying
  if echo "$result" | grep -qiE "NotAuthenticated|Authentication failed|InvalidSignature|Failed to verify"; then
    echo "AUTH FAIL"
    alert_dedup "auth" "🚨 ${SOURCE}: OCI ключ не проходит авторизацию. Проверь OCI_KEY. Продолжаю стучать (вдруг временный сбой)."
    pace_sleep
    continue
  fi

  # UNKNOWN — possible API change, alert MAX 1/hour
  short=$(echo "$result" | head -c 150 | tr '\n' ' ')
  echo "UNKNOWN: $short"
  alert_dedup "unknown" "⚠️ ${SOURCE}: странный ответ Oracle (возможно API изменился):%0A${short}%0AПродолжаю старым методом."
  pace_sleep
done

emit_stats
echo "[$SOURCE] run done"
